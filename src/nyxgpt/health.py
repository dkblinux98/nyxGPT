"""System health aggregation for the admin health dashboard.

Combines service uptime, external dependency reachability checks,
resource utilization, and threshold-based alert indicators into the
snapshot served by `GET /api/v1/admin/health` and rendered at
`/admin/health`.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Thresholds are intentionally simple fixed constants rather than a config
# section: they exist to flag obviously unhealthy states (near-exhausted
# memory/CPU/disk, elevated error rates) rather than to support fine-grained
# SLOs. The same thresholds also back the Grafana-provisioned alert rules
# (docker/grafana/provisioning/alerting/rules.yml) -- keep them in sync, since
# that's the alerting source of truth; these constants only remain here as
# the *local fallback* used when Grafana's alerting API is unreachable, see
# `fetch_grafana_alerts`.
MEMORY_WARN_PERCENT = 75.0
MEMORY_CRITICAL_PERCENT = 90.0
CPU_WARN_PERCENT = 80.0
CPU_CRITICAL_PERCENT = 95.0
DISK_WARN_PERCENT = 80.0
DISK_CRITICAL_PERCENT = 90.0
ERROR_RATE_WARN_PERCENT = 5.0
ERROR_RATE_CRITICAL_PERCENT = 15.0

# Timeout for the Grafana Alertmanager query below -- this runs inline in the
# `/api/v1/admin/health` request path, so it must fail fast and fall back to
# the local snapshot rather than hang the dashboard on an unreachable Grafana.
GRAFANA_ALERTS_TIMEOUT_S = 2.0

_START_TIME = time.time()


@dataclass
class DependencyCheck:
    """Result of a single external dependency reachability check."""

    name: str
    ok: bool
    detail: str
    applicable: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict representation of this check."""
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "applicable": self.applicable,
        }


@dataclass
class Alert:
    """A health alert, either sourced from Grafana's alerting API (the
    source of truth, see `fetch_grafana_alerts`) or computed locally as a
    fallback (see `compute_alerts`) when Grafana is disabled or unreachable.
    """

    severity: str  # "warning" | "critical"
    message: str
    source: str = "local"  # "grafana" | "local"

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable dict representation of this alert."""
        return {"severity": self.severity, "message": self.message, "source": self.source}


def uptime_seconds() -> float:
    """Seconds since this process (module) was loaded."""
    return time.time() - _START_TIME


def check_ollama(base_url: str, timeout_s: float = 2.0) -> DependencyCheck:
    """Check whether the configured Ollama backend is reachable."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s):
            return DependencyCheck(name="ollama", ok=True, detail=f"Reachable at {base_url}")
    except Exception as e:
        return DependencyCheck(name="ollama", ok=False, detail=str(e))


def check_cassandra(rag_enabled: bool) -> DependencyCheck:
    """Check Cassandra connectivity when RAG is enabled.

    RAG is opt-in, so Cassandra is not a dependency of the base system;
    when RAG is disabled this reports `applicable=False` rather than a
    failure so it doesn't trip alerts for operators not using it.
    """
    if not rag_enabled:
        return DependencyCheck(
            name="cassandra",
            ok=True,
            detail="RAG disabled; Cassandra is not required",
            applicable=False,
        )
    try:
        from nyxgpt.rag.vectorstore_cassandra import _cassandra_cfg, get_connection_pool

        cfg = _cassandra_cfg()
        pool = get_connection_pool(cfg)
        session = pool.get_session()
        session.execute("SELECT release_version FROM system.local")
        return DependencyCheck(name="cassandra", ok=True, detail="Connected")
    except Exception as e:
        return DependencyCheck(name="cassandra", ok=False, detail=str(e))


def compute_alerts(
    resource_metrics: dict[str, Any] | None, dependencies: list[DependencyCheck]
) -> list[Alert]:
    """Derive alert indicators from resource utilization and dependency checks."""
    alerts: list[Alert] = []

    if resource_metrics:
        mem_percent = resource_metrics.get("memory", {}).get("percent", 0.0)
        # System-wide, core-normalized (0-100) -- matches what Resource
        # Metrics history reports as "System" and can never exceed 100.
        # `cpu.process_percent` (this process's own usage) is NOT
        # normalized by core count and can read >100% on a multi-core
        # machine even while the system is idle, which is why it must
        # never be used for threshold alerting.
        cpu_percent = resource_metrics.get("cpu", {}).get("system_percent", 0.0)
        error_rate = resource_metrics.get("errors", {}).get("rate_percent", 0.0)

        alerts.extend(
            _threshold_alert(
                "Memory usage", mem_percent, MEMORY_WARN_PERCENT, MEMORY_CRITICAL_PERCENT
            )
        )
        alerts.extend(
            _threshold_alert("CPU usage", cpu_percent, CPU_WARN_PERCENT, CPU_CRITICAL_PERCENT)
        )
        disk_percent = resource_metrics.get("disk", {}).get("percent", 0.0)
        alerts.extend(
            _threshold_alert("Disk usage", disk_percent, DISK_WARN_PERCENT, DISK_CRITICAL_PERCENT)
        )
        alerts.extend(
            _threshold_alert(
                "Error rate", error_rate, ERROR_RATE_WARN_PERCENT, ERROR_RATE_CRITICAL_PERCENT
            )
        )

    for dep in dependencies:
        if dep.applicable and not dep.ok:
            alerts.append(
                Alert("critical", f"Dependency '{dep.name}' is unreachable: {dep.detail}")
            )

    return alerts


def fetch_grafana_alerts(cfg: ConfigParser) -> list[Alert] | None:
    """Fetch currently firing alerts from Grafana's embedded Alertmanager.

    This is the alerting source of truth (docker/grafana/provisioning/alerting)
    -- when it's reachable, the admin health dashboard should show exactly
    what Grafana itself considers firing rather than recomputing thresholds
    independently, which is how the health panel and real alerting silently
    drifted apart before.

    Returns `None` (not an empty list) when monitoring is disabled or the API
    can't be reached in time, so callers can tell "no alerts firing" apart
    from "couldn't check" and fall back to `compute_alerts`'s local snapshot
    instead of reporting a false all-clear.
    """
    from nyxgpt.config import get_monitoring_config, resolve_grafana_admin_password

    monitoring = get_monitoring_config(cfg)
    if not monitoring["enabled"]:
        return None

    # Same resolution `ops._grafana_admin_password` uses: falls back to the
    # ops-managed secret on disk when config.ini leaves the password unset
    # (the documented default install), instead of a "" that guarantees
    # every request below 401s and this source silently never activates (#3458).
    password = resolve_grafana_admin_password(cfg)

    url = monitoring["grafana_ui_url"].rstrip("/") + "/api/alertmanager/grafana/api/v2/alerts"
    try:
        response = httpx.get(
            url,
            auth=("admin", password),
            timeout=GRAFANA_ALERTS_TIMEOUT_S,
            params={"active": "true", "silenced": "false", "inhibited": "false"},
        )
        response.raise_for_status()
        raw_alerts = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            logger.warning(
                "health: Grafana alerting API at %s rejected the admin password "
                "(401) -- check ~/.nyxGPT/secrets/grafana-admin-password matches "
                "what the Grafana container is running with (see `nyxgpt ops doctor`)",
                url,
            )
        else:
            logger.warning("health: could not reach Grafana alerting API at %s: %s", url, e)
        return None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("health: could not reach Grafana alerting API at %s: %s", url, e)
        return None

    alerts: list[Alert] = []
    for raw in raw_alerts:
        labels = raw.get("labels", {})
        annotations = raw.get("annotations", {})
        severity = labels.get("severity", "warning")
        name = labels.get("alertname", "Alert")
        summary = annotations.get("summary") or f"{name} is firing"
        alerts.append(Alert(severity=severity, message=summary, source="grafana"))
    return alerts


def _threshold_alert(label: str, value: float, warn: float, critical: float) -> list[Alert]:
    """Return a critical/warning Alert for `label` if `value` crosses a threshold.

    Returns an empty list if `value` is below `warn`.
    """
    if value >= critical:
        return [Alert("critical", f"{label} at {value:.1f}% (critical threshold {critical:.0f}%)")]
    if value >= warn:
        return [Alert("warning", f"{label} at {value:.1f}% (warning threshold {warn:.0f}%)")]
    return []
