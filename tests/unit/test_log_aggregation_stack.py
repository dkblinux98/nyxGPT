"""Unit tests for the opt-in Loki/promtail log aggregation stack.

Validates the Docker Compose wiring, Loki retention config, promtail scrape
config, and the Grafana Loki datasource + SRE Home's featured logs panel.
"""

from __future__ import annotations

import json
import logging
import re
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.config import get_log_aggregation_config, get_log_aggregation_enabled
from nyxgpt.logging import DEFAULT_DATEFMT, DEFAULT_FMT

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

    loki_volumes = services["loki"]["volumes"]
    assert any(v.endswith(".nyxGPT/volumes/loki:/loki") for v in loki_volumes)

    # promtail must read from the same bind-mounted directory the api
    # service writes logs to (see docs/docker-compose.md#volumes).
    promtail_volumes = services["promtail"]["volumes"]
    assert any(
        v.endswith(".nyxGPT/volumes/nyxgpt-data:/var/log/nyxgpt:ro") for v in promtail_volumes
    )


def test_compose_wires_web_tier_log_shipping() -> None:
    """Compose-mode web logs (#3430, web/src/lib/logger.ts) must reach the
    same promtail instance the api service's logs do -- a dedicated volume
    on both the web service (write) and promtail (read) sides, plus
    NYXGPT_LOG_DIR telling the Next.js structured logger where to append."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    web_env = services["web"]["environment"]
    assert web_env["NYXGPT_LOG_DIR"] == "/var/log/nyxgpt-web"
    web_volumes = services["web"]["volumes"]
    assert any(
        v.endswith(".nyxGPT/volumes/nyxgpt-web-logs:/var/log/nyxgpt-web") for v in web_volumes
    )

    promtail_volumes = services["promtail"]["volumes"]
    assert any(
        v.endswith(".nyxGPT/volumes/nyxgpt-web-logs:/var/log/nyxgpt-web:ro")
        for v in promtail_volumes
    )


def test_compose_wires_web_tier_otel_endpoint() -> None:
    """The web service's server-side OTel exporter (#3430,
    src/instrumentation.ts) must point at the otel-collector Compose service
    by its container-network hostname -- "localhost" there would mean the
    web container itself, not the collector next to it."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    web_env = compose["services"]["web"]["environment"]

    assert web_env["NYXGPT_OTLP_ENDPOINT"] == "http://otel-collector:4318/v1/traces"


def test_loki_config_has_retention_enabled() -> None:
    loki_config = yaml.safe_load((REPO_ROOT / "docker" / "loki-config.yml").read_text())

    assert loki_config["compactor"]["retention_enabled"] is True
    assert loki_config["limits_config"]["retention_period"]


def test_promtail_scrapes_nyxgpt_log_files_and_ships_to_loki() -> None:
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())

    assert promtail_config["clients"][0]["url"] == "http://loki:3100/loki/api/v1/push"

    scrape_config = promtail_config["scrape_configs"][0]
    static_configs = scrape_config["static_configs"]
    assert static_configs, "promtail must scrape at least one target"
    for target in static_configs:
        labels = target["labels"]
        assert labels["job"] == "nyxgpt"
        assert labels["__path__"].endswith(".log*")


def test_promtail_static_configs_carry_bounded_service_name_labels() -> None:
    """Per-service `service_name` labels (#3441) so Grafana Logs Drilldown's
    service breakdown shows the real services instead of one flat
    `job=nyxgpt` bucket. The set is fixed/bounded to keep label cardinality
    safe -- web is included since #3430's web-tier structured logging."""
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())
    static_configs = promtail_config["scrape_configs"][0]["static_configs"]

    service_names = {target["labels"]["service_name"] for target in static_configs}
    assert service_names == {"api", "cli", "web", "ollama", "cassandra"}

    # api/cli must be scraped from both the Compose-mode api-container
    # directory and the native-mode host directory, since either deployment
    # mode can be the one actually writing them.
    for service in ("api", "cli"):
        paths = {
            target["labels"]["__path__"]
            for target in static_configs
            if target["labels"]["service_name"] == service
        }
        assert any(p.startswith("/var/log/nyxgpt/logs/") for p in paths)
        assert any(p.startswith("/var/log/nyxgpt-native/logs/") for p in paths)

    # ollama/cassandra are never nyxgpt Python processes -- they only ever
    # land in the native-mode directory (written by the log-follower
    # LaunchAgents, never a symlink -- see scripts/follow-ollama-logs.sh).
    for service in ("ollama", "cassandra"):
        paths = {
            target["labels"]["__path__"]
            for target in static_configs
            if target["labels"]["service_name"] == service
        }
        assert paths == {f"/var/log/nyxgpt-native/logs/{service}.log*"}


def test_promtail_scrapes_compose_mode_web_tier_logs() -> None:
    """The web-tier log path (#3430) must scrape into the same `job=nyxgpt`
    stream, with the same pipeline_stages, as the api's Compose/native paths
    -- so web logs are filterable by level/logger like everything else."""
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())
    scrape_config = promtail_config["scrape_configs"][0]

    static_configs = scrape_config["static_configs"]
    paths = [sc["labels"]["__path__"] for sc in static_configs]
    assert "/var/log/nyxgpt-web/*.log*" in paths
    for sc in static_configs:
        assert sc["labels"]["job"] == "nyxgpt"


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


def _promtail_regex() -> str:
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())
    pipeline_stages = promtail_config["scrape_configs"][0]["pipeline_stages"]
    return next(stage["regex"] for stage in pipeline_stages if "regex" in stage)["expression"]


def test_promtail_timestamp_stage_uses_utc_location() -> None:
    """nyxgpt.logging writes timestamps in UTC (`formatter.converter =
    time.gmtime`); the timestamp stage must be told so explicitly, or a
    non-UTC host's promtail parses them as UTC-when-they're-not and shifts
    every line hours into the past -- outside the curated Explore links'
    now-1h window (#3349)."""
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())
    pipeline_stages = promtail_config["scrape_configs"][0]["pipeline_stages"]
    timestamp_stage = next(stage["timestamp"] for stage in pipeline_stages if "timestamp" in stage)
    assert timestamp_stage["location"] == "UTC"


def test_promtail_regex_matches_canonical_default_fmt_line() -> None:
    """A real line produced by `nyxgpt.logging.DEFAULT_FMT` must match, with
    the extracted `logger`/`level` matching what was actually logged --
    format ↔ regex contract, so a future format change fails this test
    instead of silently blanking every curated Grafana query (#3349)."""
    expression = _promtail_regex()
    formatter = logging.Formatter(
        fmt=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT, defaults={"request_id": "-", "trace_suffix": ""}
    )
    record = logging.LogRecord(
        name="nyxgpt.self_heal",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="restart succeeded",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-abc"
    line = formatter.format(record)

    match = re.match(expression, line)
    assert match is not None
    assert match.group("logger") == "nyxgpt.self_heal"
    assert match.group("level") == "INFO"
    assert match.group("request_id") == "req-abc"
    assert match.group("message") == "restart succeeded"


def test_promtail_regex_matches_line_with_trace_context_suffix() -> None:
    """A line produced while a span is active carries a trailing `trace_id=
    <hex> span_id=<hex>` suffix (#3430, `TraceContextFilter`) -- the regex
    must still match and the suffix must land inside `message`, since that's
    exactly where Grafana's Loki derived field (`matcherRegex:
    'trace_id=(\\w+)'`) looks for it."""
    expression = _promtail_regex()
    formatter = logging.Formatter(
        fmt=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT, defaults={"request_id": "-", "trace_suffix": ""}
    )
    record = logging.LogRecord(
        name="nyxgpt.chat",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="chat request handled",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-abc"
    record.trace_suffix = " trace_id=" + "a" * 32 + " span_id=" + "b" * 16
    line = formatter.format(record)

    match = re.match(expression, line)
    assert match is not None
    assert match.group("logger") == "nyxgpt.chat"
    assert "trace_id=" + "a" * 32 in match.group("message")


def test_promtail_regex_matches_millisecond_no_bracket_variant() -> None:
    """Real native-install log files have been observed with a second line
    shape: comma-millisecond timestamp, no `[request_id]` bracket. Lines in
    this shape must still yield `logger`/`level` labels (#3349), or they're
    invisible to every curated query, which filters on those labels."""
    expression = _promtail_regex()
    line = "2026-07-25 21:21:57,596 DEBUG nyxgpt.api: handling request"

    match = re.match(expression, line)
    assert match is not None
    assert match.group("logger") == "nyxgpt.api"
    assert match.group("level") == "DEBUG"
    assert match.group("timestamp") == "2026-07-25 21:21:57"
    assert match.group("message") == "handling request"


def test_operational_logs_dashboard_is_retired() -> None:
    """Superseded by Grafana's Logs Drilldown app, which replaces the
    curated per-component saved queries (#3411) -- the standalone dashboard
    and its Dashboard Catalog entry are gone."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    assert not (dashboards_dir / "operational-logs.json").exists()


def test_grafana_has_loki_datasource() -> None:
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    loki_datasource = next(ds for ds in datasource_config["datasources"] if ds["name"] == "Loki")
    assert loki_datasource["type"] == "loki"
    assert loki_datasource["url"] == "http://loki:3100"


def test_logs_explorer_dashboard_is_retired() -> None:
    """Superseded by Grafana's Logs Drilldown app plus a featured, queryless
    logs panel on SRE Home itself (#3424) -- the standalone dashboard is
    gone, same as the earlier Operational Logs retirement above."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    assert not (dashboards_dir / "logs-explorer.json").exists()


def test_sre_home_features_a_queryless_logs_panel() -> None:
    """Owner acceptance (#3424): logs must be a featured, queryless
    `{job="nyxgpt"}` panel on SRE Home itself, not just a link out to
    Logs Drilldown."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "sre-home.json").read_text())

    logs_panels = [p for p in dashboard["panels"] if p["type"] == "logs"]
    assert logs_panels, "SRE Home must render a featured logs panel, not just a link"
    for panel in logs_panels:
        for target in panel["targets"]:
            assert target["datasource"]["type"] == "loki"
            assert target["expr"] == '{job="nyxgpt"}'


def test_sre_home_logs_panel_links_directly_into_prefiltered_drilldown() -> None:
    """Owner acceptance (#3473): the SPOG featured logs panel must carry a
    working title-corner link straight into Grafana's Logs Drilldown app,
    pre-filtered to `job=nyxgpt` -- Grafana's own left-nav entry to
    Drilldown can't be provisioned pre-filtered, so every nyxGPT-owned
    entry point (this one included) must carry the filter itself."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "sre-home.json").read_text())

    logs_panel = next(p for p in dashboard["panels"] if p["type"] == "logs")
    links = logs_panel.get("links")
    assert links, "the featured logs panel must have a title-corner link into Drilldown"

    drilldown_link = links[0]
    assert drilldown_link["url"].startswith("/a/grafana-lokiexplore-app/")
    assert "var-ds=loki" in drilldown_link["url"]
    assert "var-filters=job%7C%3D%7Cnyxgpt" in drilldown_link["url"]


def test_sre_home_title_is_trimmed() -> None:
    """Owner acceptance (#3473): the header panel's title is exactly
    "nyxGPT SRE Home" -- the "-- single pane of glass" suffix comes off."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "sre-home.json").read_text())

    header_panel = next(p for p in dashboard["panels"] if p["id"] == 1)
    assert header_panel["title"] == "nyxGPT SRE Home"
    assert "single pane of glass" not in json.dumps(dashboard)


def test_sre_home_has_no_ordering_blurb() -> None:
    """Owner acceptance (#3473): the "Live health and metrics below, then
    full log search, then traces and errors." ordering blurb comes out
    entirely -- the layout should speak for itself."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    text = (dashboards_dir / "sre-home.json").read_text()
    assert "Live health and metrics below" not in text


def test_sre_home_deep_dive_links_sit_above_their_own_sections() -> None:
    """Owner acceptance (#3473): deep-dive dashboard links are no longer
    pooled in one grouped block -- each sits in its own panel, positioned
    (by gridPos y) directly above the section of panels it deepens, not
    bundled into the header panel."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "sre-home.json").read_text())
    panels = dashboard["panels"]
    header_panel = next(p for p in panels if p["id"] == 1)

    assert header_panel["options"]["content"] == "", "header panel must not bundle any links"

    # (deep-dive dashboard uid fragment, titles of the panels it deepens)
    sections = [
        ("nyxgpt-system-overview", ["API up", "Request rate"]),
        ("nyxgpt-api-metrics", ["API up", "Request rate"]),
        ("nyxgpt-rag-performance", ["RAG query rate by source"]),
        ("nyxgpt-resource-usage", ["Memory (RSS)"]),
        ("nyxgpt-self-healing", ["Unhealthy components (now)"]),
        ("nyxgpt-canary", ["Canary rollout active"]),
    ]

    text_panels = [p for p in panels if p["type"] == "text"]
    for dashboard_uid, section_panel_titles in sections:
        owning_panels = [p for p in text_panels if f"/d/{dashboard_uid}" in p["options"]["content"]]
        assert owning_panels, f"no deep-dive link panel found for {dashboard_uid!r}"
        assert len(owning_panels) == 1, f"{dashboard_uid!r} link must not be duplicated"
        link_panel = owning_panels[0]
        assert (
            link_panel["id"] != 1
        ), f"{dashboard_uid!r} link must not be bundled into the header panel"

        link_y = link_panel["gridPos"]["y"]
        for title in section_panel_titles:
            section_panel = next(p for p in panels if p.get("title") == title)
            assert link_y < section_panel["gridPos"]["y"], (
                f"{dashboard_uid!r} deep-dive link must sit above its section " f"({title!r})"
            )


def test_sre_home_dashboard_is_provisioned_and_is_the_landing_page() -> None:
    """The SRE Home dashboard (#3411) is the single pane of glass the Admin
    Dashboard's SRE Overview tile opens -- it must be provisioned, and set
    as Grafana's org/home default so it's what you land on."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "sre-home.json").read_text())

    assert dashboard["uid"] == "nyxgpt-sre-home"
    panel_types = {panel["type"] for panel in dashboard["panels"]}
    assert "traces" in panel_types, "must give traces a default, non-blank view"

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    grafana_env = compose["services"]["grafana"]["environment"]
    assert grafana_env["GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH"] == (
        "/var/lib/grafana/dashboards/sre-home.json"
    )


def test_grafana_plugins_are_provisioned_on_a_fresh_install() -> None:
    """Logs Drilldown and the Infinity datasource (GlitchTip panels) must be
    guaranteed present/enabled on a fresh install (#3411), not assumed
    bundled in a given Grafana image build."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    plugins = compose["services"]["grafana"]["environment"]["GF_INSTALL_PLUGINS"]

    assert "grafana-lokiexplore-app" in plugins
    assert "yesoreyeram-infinity-datasource" in plugins


def test_grafana_has_jaeger_datasource() -> None:
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    jaeger_datasource = next(
        ds for ds in datasource_config["datasources"] if ds["name"] == "Jaeger"
    )
    assert jaeger_datasource["type"] == "jaeger"
    assert jaeger_datasource["uid"] == "jaeger"
    assert jaeger_datasource["url"] == "http://jaeger:16686"


def test_loki_has_a_derived_field_linking_to_jaeger() -> None:
    """Loki<->trace linkage config (#3411): a derived-field extraction for
    `trace_id` pointing at the Jaeger datasource. Inert until #3415 stamps
    `trace_id` into log lines, but must be present and correctly wired."""
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    loki_datasource = next(ds for ds in datasource_config["datasources"] if ds["name"] == "Loki")
    derived_fields = loki_datasource["jsonData"]["derivedFields"]

    assert any(
        f["datasourceUid"] == "jaeger" and "trace_id" in f["matcherRegex"] for f in derived_fields
    )


def test_grafana_has_glitchtip_infinity_datasource() -> None:
    """GlitchTip is queried via its Sentry-compatible REST API through the
    Infinity datasource plugin (#3411, owner-selected option), authenticated
    with the token `nyxgpt ops glitchtip-init` mints -- never a hand-pasted
    token. Lives in its own provisioning file, glitchtip.yml, separate from
    datasource.yml (#3432): Grafana 13 fails an entire provisioning file
    when one datasource in it can't interpolate, so keeping GlitchTip's
    `$__file{}` token reference isolated means Prometheus/Loki/Jaeger in
    datasource.yml survive even if the token file is missing."""
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "glitchtip.yml"
        ).read_text()
    )
    glitchtip_datasource = next(
        ds for ds in datasource_config["datasources"] if ds["name"] == "GlitchTip"
    )
    assert glitchtip_datasource["type"] == "yesoreyeram-infinity-datasource"
    assert glitchtip_datasource["jsonData"]["auth_method"] == "bearerToken"
    assert "glitchtip-grafana-token" in glitchtip_datasource["secureJsonData"]["bearerToken"]


def test_datasource_yml_no_longer_declares_glitchtip() -> None:
    """Regression guard for #3432: GlitchTip must stay out of datasource.yml
    so a missing/broken token can never take Prometheus/Loki/Jaeger down
    with it (Grafana 13 fails per-file, not per-datasource-entry)."""
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    names = {ds["name"] for ds in datasource_config["datasources"]}
    assert names == {"Prometheus", "Loki", "Jaeger"}


def test_grafana_mounts_the_glitchtip_token_secret_read_only() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["grafana"]["volumes"]
    assert any(v.endswith(".nyxGPT/secrets:/etc/nyxgpt-secrets:ro") for v in volumes)


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
    assert {"Self-heal events", "Canary events", "Chat errors"} <= labels
    for q in curated_queries:
        assert q["query"].startswith('{job="nyxgpt"')
        assert q["hint"]
