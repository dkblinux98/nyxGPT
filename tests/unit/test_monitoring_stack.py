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
from nyxgpt.config import (
    get_error_tracking_config,
    get_monitoring_config,
    get_monitoring_enabled,
    get_tracing_config,
)

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
    "nyxgpt_selfheal_component_healthy",
    "nyxgpt_selfheal_last_check_timestamp",
    "nyxgpt_selfheal_restarts_total",
    "nyxgpt_selfheal_restart_count",
    "nyxgpt_selfheal_last_recovery_timestamp",
    "nyxgpt_ops_actions_total",
    "nyxgpt_canary_rollout_active",
    "nyxgpt_canary_weight_percent",
    "nyxgpt_canary_evaluations_total",
    "nyxgpt_canary_events_total",
    "nyxgpt_canary_track_version_info",
    "nyxgpt_canary_component_rollout_active",
    "nyxgpt_canary_component_weight_percent",
    "nyxgpt_canary_component_evaluations_total",
    "nyxgpt_canary_component_events_total",
    "nyxgpt_canary_component_track_version_info",
    "nyxgpt_rag_ingests_total",
    "nyxgpt_cache_requests_total",
    "nyxgpt_rate_limit_rejections_total",
    "nyxgpt_resource_memory_rss_mb",
    "nyxgpt_resource_memory_percent",
    "nyxgpt_resource_cpu_percent",
    "nyxgpt_resource_disk_percent",
    "nyxgpt_resource_queue_depth",
    "nyxgpt_selfheal_giveup_total",
    "nyxgpt_canary_auto_rollback_total",
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
        "nyxgpt_selfheal_component_healthy",
        "nyxgpt_selfheal_last_check_timestamp",
        "nyxgpt_selfheal_restarts_total",
        "nyxgpt_selfheal_restart_count",
        "nyxgpt_selfheal_last_recovery_timestamp",
        "nyxgpt_ops_actions_total",
        "nyxgpt_canary_rollout_active",
        "nyxgpt_canary_weight_percent",
        "nyxgpt_canary_evaluations_total",
        "nyxgpt_canary_events_total",
        "nyxgpt_canary_track_version_info",
        "nyxgpt_canary_component_rollout_active",
        "nyxgpt_canary_component_weight_percent",
        "nyxgpt_canary_component_evaluations_total",
        "nyxgpt_canary_component_events_total",
        "nyxgpt_canary_component_track_version_info",
        "nyxgpt_rag_ingests_total",
        "nyxgpt_cache_requests_total",
        "nyxgpt_rate_limit_rejections_total",
        "nyxgpt_resource_memory_rss_mb",
        "nyxgpt_resource_memory_percent",
        "nyxgpt_resource_cpu_percent",
        "nyxgpt_resource_disk_percent",
        "nyxgpt_resource_queue_depth",
        "nyxgpt_selfheal_giveup_total",
        "nyxgpt_canary_auto_rollback_total",
    }


def test_compose_defines_opt_in_monitoring_profile() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    for name in ("prometheus", "grafana"):
        assert name in services, f"{name} service missing from docker-compose.yml"
        assert services[name]["profiles"] == [
            "monitoring"
        ], f"{name} must only start under the opt-in 'monitoring' profile"

    prometheus_volumes = services["prometheus"]["volumes"]
    assert any(v.endswith(".nyxGPT/volumes/prometheus:/prometheus") for v in prometheus_volumes)
    grafana_volumes = services["grafana"]["volumes"]
    assert any(v.endswith(".nyxGPT/volumes/grafana:/var/lib/grafana") for v in grafana_volumes)


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
    # In the local-first layout the API is a native brew service, not a
    # Compose service, so there is no `api` hostname on the observability
    # network -- the target must go through the host gateway instead.
    prometheus_config = yaml.safe_load((REPO_ROOT / "docker" / "prometheus.yml").read_text())
    scrape_configs = prometheus_config["scrape_configs"]

    nyxgpt_job = next(job for job in scrape_configs if job["job_name"] == "nyxgpt-api")
    assert nyxgpt_job["metrics_path"] == "/metrics"
    targets = nyxgpt_job["static_configs"][0]["targets"]
    assert targets == ["host.docker.internal:8000"]


def test_prometheus_service_maps_host_docker_internal_for_plain_linux_engines() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    extra_hosts = compose["services"]["prometheus"]["extra_hosts"]
    assert "host.docker.internal:host-gateway" in extra_hosts


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
        REPO_ROOT / "docker" / "grafana" / "provisioning" / "alerting" / "rules.yml",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "system-overview.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "rag-performance.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "api-metrics.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "self-healing.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "canary.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "resource-usage.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json",
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


LOCAL_TRAFFIC_PANELS = {
    "docker/grafana/dashboards/sre-home.json": [
        "Requests by status",
        "5xx errors",
        "RAG queries by source",
        "RAG ingests by source and outcome",
        "Chat requests (streaming vs. non-streaming)",
    ],
    "docker/grafana/dashboards/rag-performance.json": [
        "RAG queries by source",
        "Chat requests (streaming vs. non-streaming)",
        "RAG ingests by source and outcome",
    ],
}


@pytest.mark.parametrize(
    "dashboard_name, panel_title",
    [
        (dashboard_name, panel_title)
        for dashboard_name, panel_titles in LOCAL_TRAFFIC_PANELS.items()
        for panel_title in panel_titles
    ],
)
def test_local_traffic_panels_use_increase_not_bare_rate(
    dashboard_name: str, panel_title: str
) -> None:
    """A handful of interactive queries/ingests on a single-user local
    instance divide down to a near-invisible fraction of a request/second
    under `rate(...[5m])` plotted against a `reqps` unit (#3469). These
    panels must use `increase(...[$__rate_interval])` -- a visible integer
    count over the dashboard window -- with a `short` unit instead, so a
    revert back to `rate(`/`reqps` fails this test rather than shipping the
    illegibility bug again undetected."""
    dashboard = json.loads((REPO_ROOT / dashboard_name).read_text())
    panels = {p["title"]: p for p in dashboard["panels"]}
    assert panel_title in panels, f"{dashboard_name} is missing panel {panel_title!r}"
    panel = panels[panel_title]

    assert panel["fieldConfig"]["defaults"]["unit"] == "short", (
        f"{dashboard_name}::{panel_title} unit should be 'short', "
        "not a per-second rate unit like 'reqps'"
    )

    assert panel["targets"], f"{dashboard_name}::{panel_title} has no targets"
    for target in panel["targets"]:
        expr = target["expr"]
        assert (
            "increase(" in expr
        ), f"{dashboard_name}::{panel_title} expr should use increase(): {expr!r}"
        assert (
            "rate(" not in expr
        ), f"{dashboard_name}::{panel_title} expr should not use a bare rate(): {expr!r}"


RAG_TOTAL_STAT_PANELS = [
    "Total RAG queries by source",
    "Total RAG ingests by source",
]


@pytest.mark.parametrize("panel_title", RAG_TOTAL_STAT_PANELS)
def test_rag_total_by_source_panels_render_honest_zero(panel_title: str) -> None:
    """A Grafana `piechart` panel draws nothing for an all-zero series --
    Prometheus (correctly) exposes it, but the panel shows a blank tile
    indistinguishable from "No data" (#3469 acceptance failure: the owner
    saw the RAG-queries total panel render blank despite real queries that
    session, on a dashboard whose whole point is the #3424 honest-zero
    convention). A `stat` panel renders each series' value explicitly,
    including 0, so this must stay `stat` and never regress back to
    `piechart`."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "rag-performance.json").read_text()
    )
    panels = {p["title"]: p for p in dashboard["panels"]}
    assert panel_title in panels, f"rag-performance.json is missing panel {panel_title!r}"
    assert panels[panel_title]["type"] == "stat", (
        f"{panel_title} should be a 'stat' panel so legitimate all-zero series "
        "render as an honest 0 instead of a blank piechart"
    )


def test_grafana_dashboards_are_provisioned() -> None:
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard_files = sorted(p.name for p in dashboards_dir.glob("*.json"))
    assert dashboard_files == [
        "api-metrics.json",
        "canary.json",
        "rag-performance.json",
        "resource-usage.json",
        "self-healing.json",
        "sre-home.json",
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


def _iter_panel_datasource_refs(obj: object):
    if isinstance(obj, dict):
        if "uid" in obj and "type" in obj and set(obj) <= {"uid", "type"}:
            yield obj
        for value in obj.values():
            yield from _iter_panel_datasource_refs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_panel_datasource_refs(item)


@pytest.mark.parametrize(
    "dashboard_path",
    sorted((REPO_ROOT / "docker" / "grafana" / "dashboards").glob("*.json")),
)
def test_dashboard_datasource_uids_are_all_provisioned(dashboard_path: Path) -> None:
    """Every `{"type": ..., "uid": ...}` datasource reference in a dashboard
    must resolve to a uid actually declared across the datasources
    provisioning dir -- catches a dashboard referencing a
    renamed/typo'd/removed datasource before it ships as a silent
    "Datasource was not found" in Grafana (#3424). Scans every `*.yml` in
    the dir, not just datasource.yml: GlitchTip lives in its own
    glitchtip.yml (#3432) so a bad `$__file{}` token interpolation can't
    take Prometheus/Loki/Jaeger down with it."""
    datasources_dir = REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources"
    provisioned_uids = {
        ds["uid"]
        for path in sorted(datasources_dir.glob("*.yml"))
        for ds in yaml.safe_load(path.read_text())["datasources"]
    }

    dashboard = json.loads(dashboard_path.read_text())
    refs = list(_iter_panel_datasource_refs(dashboard))
    assert refs, f"expected at least one datasource reference in {dashboard_path.name}"
    for ref in refs:
        assert (
            ref["uid"] in provisioned_uids
        ), f"{dashboard_path.name} references undeclared datasource uid {ref['uid']!r}"


@pytest.mark.parametrize(
    "dashboard_path",
    sorted((REPO_ROOT / "docker" / "grafana" / "dashboards").glob("*.json")),
)
def test_dashboard_json_has_no_forbidden_strings(dashboard_path: Path) -> None:
    """No GitHub issue references and no dangling "Logs Explorer" mentions
    in dashboard text (#3424 acceptance criteria) -- dashboards are
    user-facing product surface, not commit history."""
    text = dashboard_path.read_text()
    assert not re.search(
        r"#\d{3,5}", text
    ), f"{dashboard_path.name} references a GitHub issue number in dashboard text"
    assert "Logs Explorer" not in text, f"{dashboard_path.name} still references Logs Explorer"


def test_sre_home_glitchtip_open_issues_stat_panel_reduces_over_all_fields() -> None:
    """Regression test for #3424: the Infinity table result's only column
    (`id`) is typed `string`, and a Stat panel's default `reduceOptions`
    only reduces over numeric fields -- with none, it silently renders "No
    data" even though the paired table panel querying the exact same URL
    populates fine. `reduceOptions.fields` must be widened past the numeric
    default so the count actually renders."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json").read_text()
    )
    panel = next(p for p in dashboard["panels"] if p["title"].startswith("GlitchTip: open issues"))
    assert panel["type"] == "stat"
    assert panel["options"]["reduceOptions"]["fields"] != ""


def test_sre_home_glitchtip_panels_filter_to_unresolved_issues() -> None:
    """Regression test for #3470: GlitchTip's issues endpoint returns every
    issue regardless of resolution status unless the query carries an
    `is:unresolved` filter, so a dashboard querying without it shows the
    all-time issue count (e.g. "50 open issues") instead of matching
    GlitchTip's own UI, which defaults to the unresolved view. Both the
    "open issues" stat and the "recent errors" table must request the
    filter so neither surfaces resolved issues as open."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json").read_text()
    )
    glitchtip_panels = [p for p in dashboard["panels"] if p["title"].startswith("GlitchTip:")]
    assert len(glitchtip_panels) == 2
    for panel in glitchtip_panels:
        for target in panel["targets"]:
            assert (
                "query=is%3Aunresolved" in target["url"]
            ), f"{panel['title']!r} issues query is missing the unresolved status filter"


def test_sre_home_dashboard_version_bumped_past_stale_glitchtip_deploy() -> None:
    """Regression test for #3565: the #3470 `is:unresolved` fix landed in
    sre-home.json (version 5) without bumping the dashboard's top-level
    `"version"`. With `allowUiUpdates: true` (dashboards.yml), Grafana's file
    provisioner only re-imports a dashboard when the file's `"version"` is
    higher than what's already stored -- an already-provisioned Grafana
    instance kept serving the stale, unfiltered query indefinitely even
    though the file on disk was already correct. `"version"` must move past
    5 so a live instance actually picks up the fix."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json").read_text()
    )
    assert dashboard["version"] > 5


def test_dashboards_provisioning_documents_the_version_bump_requirement() -> None:
    """#3565: `allowUiUpdates: true` silently drops a dashboard file update
    whose `"version"` wasn't bumped (see the regression above) -- document
    that gotcha next to the setting so the next dashboard edit doesn't
    reintroduce it."""
    text = (
        REPO_ROOT / "docker" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
    ).read_text()
    assert "allowUiUpdates: true" in text
    assert "version" in text.lower()


def test_sre_home_glitchtip_recent_errors_table_shows_status() -> None:
    """The "recent errors" table also carries a status column (#3470) so an
    issue that gets resolved between dashboard refreshes isn't mistaken for
    open before the panel next reloads."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json").read_text()
    )
    panel = next(
        p for p in dashboard["panels"] if p["title"].startswith("GlitchTip: recent errors")
    )
    selectors = {c["selector"] for c in panel["targets"][0]["columns"]}
    assert "status" in selectors


def test_glitchtip_worker_has_a_healthcheck() -> None:
    """Regression test for #3565: `glitchtip-worker` runs the actual
    event-ingestion pipeline (Celery worker + beat) but had no healthcheck,
    so `self_heal._docker_compose_container_statuses` (which treats an empty
    `Health` field as healthy whenever the container `state == "running"`)
    could never detect a dead/wedged worker -- the container process stayed
    "running" while ingestion silently stopped, with self-heal never
    restarting it. `celery inspect ping` reaches the worker process itself,
    not just the container, so a wedged worker now surfaces as unhealthy."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    worker = compose["services"]["glitchtip-worker"]

    assert (
        "healthcheck" in worker
    ), "glitchtip-worker has no healthcheck -- self-heal can't detect a dead Celery worker"
    test = worker["healthcheck"]["test"]
    assert "celery" in " ".join(test) and "inspect" in " ".join(test)


@pytest.mark.parametrize(
    "dashboard_name",
    ["self-healing.json", "sre-home.json"],
)
def test_unhealthy_components_stat_is_paired_with_a_naming_panel(dashboard_name: str) -> None:
    """#3575: a bare "Unhealthy components (now)" count can't say which
    component it means -- each dashboard showing that stat must also carry a
    panel that names the offender via the labeled per-service gauge."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / dashboard_name).read_text()
    )
    panels = {p["title"]: p for p in dashboard["panels"]}

    assert "Unhealthy components (now)" in panels
    naming_panel = panels["Unhealthy components by name (now)"]
    exprs = [t["expr"] for t in naming_panel["targets"]]
    assert any("nyxgpt_selfheal_component_healthy" in expr for expr in exprs)
    assert "{{service}}" in naming_panel["targets"][0]["legendFormat"]


@pytest.mark.parametrize(
    "dashboard_name",
    ["self-healing.json", "sre-home.json"],
)
def test_unhealthy_components_stat_states_its_freshness(dashboard_name: str) -> None:
    """#3575 AC: the Grafana panel must state its as-of time, since the
    underlying gauge holds its value between self-heal check passes rather
    than updating live."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / dashboard_name).read_text()
    )
    panels = {p["title"]: p for p in dashboard["panels"]}

    freshness_panel = panels["Self-heal check freshness"]
    exprs = [t["expr"] for t in freshness_panel["targets"]]
    assert any("nyxgpt_selfheal_last_check_timestamp" in expr for expr in exprs)


def _cfg(**monitoring_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if monitoring_options:
        cfg["monitoring"] = monitoring_options
    return cfg


def test_sre_home_panel_title_links_open_underlying_tool_uis_in_new_tabs() -> None:
    """Owner design decision for #3471 (2026-07-30): no top-of-dashboard link
    row -- Jaeger, GlitchTip, and Prometheus are each reachable via a
    Grafana panel-title link on the section they back (traces panel,
    GlitchTip panels, and the overview panel heading the health/metrics
    section, which has no single dedicated panel of its own). The
    dashboard JSON is static and can't read `config.ini` at provisioning
    time, so the linked URLs are hardcoded to the same defaults
    `nyxgpt.config` falls back to for the in-app pages -- this test fails
    if those defaults ever drift apart."""
    dashboard = json.loads(
        (REPO_ROOT / "docker" / "grafana" / "dashboards" / "sre-home.json").read_text()
    )
    panels = {p["title"]: p for p in dashboard["panels"]}

    tracing_defaults = get_tracing_config(_cfg())
    error_tracking_defaults = get_error_tracking_config(_cfg())
    monitoring_defaults = get_monitoring_config(_cfg())

    def assert_single_link(panel_title: str, expected_url: str) -> None:
        links = panels[panel_title]["links"]
        assert len(links) == 1
        assert links[0]["url"] == expected_url
        assert links[0]["targetBlank"] is True

    assert_single_link("Recent nyxgpt traces", tracing_defaults["jaeger_ui_url"])
    assert_single_link(
        "GlitchTip: open issues (nyxgpt-backend)", error_tracking_defaults["glitchtip_ui_url"]
    )
    assert_single_link(
        "GlitchTip: recent errors (nyxgpt-backend)", error_tracking_defaults["glitchtip_ui_url"]
    )
    assert_single_link("nyxGPT SRE Home", monitoring_defaults["prometheus_ui_url"])

    # The old top-of-dashboard GlitchTip text link is absorbed into the
    # GlitchTip panels' title links above, not duplicated in the markdown.
    overview_panel = panels["nyxGPT SRE Home"]
    assert "glitchtip" not in overview_panel["options"]["content"].lower()


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


# ---------------------------------------------------------------------------
# Grafana alerting provisioning (#3466)
# ---------------------------------------------------------------------------

ALERTING_DIR = REPO_ROOT / "docker" / "grafana" / "provisioning" / "alerting"


def test_prometheus_no_longer_runs_its_own_rule_evaluation() -> None:
    """Alert rules are now provisioned in Grafana (rules.yml), not Prometheus
    -- docker/prometheus-alerts.yml is gone and prometheus.yml must not
    reference a rule_files entry that no longer exists."""
    assert not (REPO_ROOT / "docker" / "prometheus-alerts.yml").exists()

    prometheus_config = yaml.safe_load((REPO_ROOT / "docker" / "prometheus.yml").read_text())
    assert "rule_files" not in prometheus_config

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    prometheus_volumes = compose["services"]["prometheus"]["volumes"]
    assert not any("alerts.yml" in v for v in prometheus_volumes)


def test_alerting_provisioning_files_exist_and_parse() -> None:
    for name in ("rules.yml", "contact-points.yml", "notification-policies.yml"):
        path = ALERTING_DIR / name
        assert path.exists(), f"missing {path}"
        doc = yaml.safe_load(path.read_text())
        assert doc["apiVersion"] == 1


def test_alert_rules_reference_provisioned_datasources() -> None:
    """Every rule's `datasourceUid` must be either the built-in expression
    datasource (`__expr__`) or a uid actually declared in the datasources
    provisioning dir -- catches a typo'd/renamed datasource before it ships
    as a silently-broken alert rule (mirrors the dashboard equivalent,
    `test_dashboard_datasource_uids_are_all_provisioned`)."""
    datasources_dir = REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources"
    provisioned_uids = {
        ds["uid"]
        for path in sorted(datasources_dir.glob("*.yml"))
        for ds in yaml.safe_load(path.read_text())["datasources"]
    }

    rules_doc = yaml.safe_load((ALERTING_DIR / "rules.yml").read_text())
    groups = rules_doc["groups"]
    assert groups, "expected at least one alert rule group"

    seen_uids: set[str] = set()
    for group in groups:
        for rule in group["rules"]:
            seen_uids.add(rule["uid"])
            for query in rule["data"]:
                ds_uid = query["datasourceUid"]
                assert ds_uid == "__expr__" or ds_uid in provisioned_uids, (
                    f"rule {rule['title']!r} references undeclared datasource " f"uid {ds_uid!r}"
                )

    assert len(seen_uids) == sum(len(g["rules"]) for g in groups), "rule uids must be unique"


def test_notification_policy_routes_to_a_provisioned_contact_point() -> None:
    contact_points_doc = yaml.safe_load((ALERTING_DIR / "contact-points.yml").read_text())
    contact_point_names = {cp["name"] for cp in contact_points_doc["contactPoints"]}

    policies_doc = yaml.safe_load((ALERTING_DIR / "notification-policies.yml").read_text())
    for policy in policies_doc["policies"]:
        assert policy["receiver"] in contact_point_names


def test_slack_contact_point_reads_webhook_url_from_the_shared_secrets_mount() -> None:
    """The webhook URL must come from the same `/etc/nyxgpt-secrets` mount
    docker-compose.yml already gives Grafana for the GlitchTip token (#3432),
    not a new bind mount -- otherwise Grafana can't resolve the `$__file{}`
    reference at all."""
    contact_points_doc = yaml.safe_load((ALERTING_DIR / "contact-points.yml").read_text())
    receiver = contact_points_doc["contactPoints"][0]["receivers"][0]
    assert receiver["type"] == "slack"
    # Must live under `settings`, not `secureSettings` (#3538): Grafana's
    # alerting-provisioning validator doesn't recognize a `secureSettings.url`
    # as "configured" at all -- confirmed by booting grafana/grafana:13.1.1
    # against this file, which crash-loops with `secureSettings.url` but
    # boots clean with `settings.url` (Grafana still redacts it in API
    # responses regardless of which section declares it).
    assert "url" not in receiver.get("secureSettings", {})
    url_ref = receiver["settings"]["url"]
    assert url_ref.startswith("$__file{/etc/nyxgpt-secrets/")

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    grafana_volumes = compose["services"]["grafana"]["volumes"]
    assert any(v.endswith(":/etc/nyxgpt-secrets:ro") for v in grafana_volumes)
