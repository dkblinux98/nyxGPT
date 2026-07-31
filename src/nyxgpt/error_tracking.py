"""Self-hosted error tracking for the nyxGPT API.

Error tracking is opt-in and local-only: exceptions are reported via the
Sentry SDK protocol to a self-hosted, Sentry-compatible tracker (e.g.
GlitchTip) that only exists when the `errors` Compose profile is running
and a local DSN is configured in config.ini. There is no default DSN and
nothing here ever talks to Sentry's own SaaS, so a fresh install stays
fully inert unless an operator explicitly opts in with a DSN pointed at
their own instance. When disabled (the default), every function in this
module is a no-op so the rest of the app pays no cost.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from nyxgpt.optional_imports import try_import, try_import_attr

_LogLevel = Literal["fatal", "critical", "error", "warning", "info", "debug"]

logger = logging.getLogger(__name__)

_enabled = False

# sentry_sdk and its integrations are separate concerns from an import-crash
# perspective: a venv can be missing one after a pull adds/bumps it (#3487).
# Guarded so that importing this module never crashes; init_error_tracking()
# checks for None and disables error tracking with a single actionable
# warning instead.
sentry_sdk = try_import("sentry_sdk")
FastApiIntegration = try_import_attr("sentry_sdk.integrations.fastapi", "FastApiIntegration")
StarletteIntegration = try_import_attr("sentry_sdk.integrations.starlette", "StarletteIntegration")

# GlitchTip always issues DSNs using [error_tracking]'s GLITCHTIP_DOMAIN host
# (localhost), since that's what a browser needs to reach its UI. Pasted
# verbatim into a *containerized* API's config.ini, that DSN is unreachable:
# "localhost" inside the api container means the api container itself, not
# the glitchtip container next to it on the Compose network. This is the
# Compose service name it needs to resolve to instead (see docker-compose.yml).
_GLITCHTIP_COMPOSE_SERVICE = "glitchtip"
_LOCALHOST_HOSTS = {"localhost", "127.0.0.1"}


def _running_in_compose_container() -> bool:
    """Whether this process is the Compose `api` container.

    NYXGPT_COMPOSE_FILE is only set for the containerized `api` service (see
    its `environment:` block in docker-compose.yml) -- a native deployment
    never sets it.
    """
    return bool(os.environ.get("NYXGPT_COMPOSE_FILE", "").strip())


def _normalize_dsn_host(dsn: str) -> str:
    """Rewrite a GlitchTip-issued DSN's host for the current deployment.

    A no-op for native deployments (where "localhost" is already correct)
    and for DSNs that don't point at localhost. In a Compose deployment, an
    unchanged localhost DSN silently fails every send -- the SDK can't reach
    the tracker -- so this is what keeps a DSN copied straight from the
    GlitchTip UI working in both deployment modes without manual editing.
    """
    if not _running_in_compose_container():
        return dsn

    parts = urlsplit(dsn)
    if parts.hostname not in _LOCALHOST_HOSTS:
        return dsn

    userinfo = parts.username or ""
    if parts.password:
        userinfo += f":{parts.password}"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{userinfo}@" if userinfo else ""
    netloc += f"{_GLITCHTIP_COMPOSE_SERVICE}{port}"

    rewritten = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    logger.info(
        "Rewrote error tracking DSN host %r -> %r for the Compose deployment",
        parts.hostname,
        _GLITCHTIP_COMPOSE_SERVICE,
        extra={"component": "error_tracking"},
    )
    return rewritten


def init_error_tracking(error_tracking_config: dict[str, Any]) -> None:
    """Set up the Sentry SDK against a local tracker, if enabled.

    No-ops unless both `[error_tracking] enabled = true` and a `dsn` are
    configured, so a fresh install never reports anything anywhere.
    """
    global _enabled

    if not error_tracking_config.get("enabled"):
        _enabled = False
        return

    dsn = (error_tracking_config.get("dsn") or "").strip()
    if not dsn:
        logger.warning(
            "Error tracking enabled but no DSN configured; leaving it disabled",
            extra={"component": "error_tracking"},
        )
        _enabled = False
        return

    if sentry_sdk is None or FastApiIntegration is None or StarletteIntegration is None:
        logger.warning(
            "Error tracking is enabled but the sentry-sdk package is not installed -- "
            "your environment is likely stale after a pull; run `pip install -e .` "
            "to install it. Error tracking stays disabled until then.",
            extra={"component": "error_tracking"},
        )
        _enabled = False
        return

    dsn = _normalize_dsn_host(dsn)

    sentry_sdk.init(
        dsn=dsn,
        environment=error_tracking_config.get("environment", "development"),
        release=error_tracking_config.get("release") or None,
        traces_sample_rate=float(error_tracking_config.get("traces_sample_rate", 0.0)),
        integrations=[StarletteIntegration(), FastApiIntegration()],
        send_default_pii=False,
    )

    _enabled = True
    logger.info(
        "Error tracking enabled",
        extra={
            "component": "error_tracking",
            "environment": error_tracking_config.get("environment"),
        },
    )


def is_error_tracking_enabled() -> bool:
    """Whether error tracking was actually initialized for this process."""
    return _enabled


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Report an exception to the local error tracker, enriched with context.

    No-op when error tracking is disabled, so call sites don't need to
    special-case it.
    """
    if not _enabled or sentry_sdk is None:
        return

    with sentry_sdk.isolation_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_exception(exc)


def capture_message(message: str, *, level: _LogLevel = "error", **context: Any) -> None:
    """Report a message-level event (e.g. a web UI client error) to the tracker.

    No-op when error tracking is disabled.
    """
    if not _enabled or sentry_sdk is None:
        return

    with sentry_sdk.isolation_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level=level)
