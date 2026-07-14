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
from typing import Any, Literal

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

_LogLevel = Literal["fatal", "critical", "error", "warning", "info", "debug"]

logger = logging.getLogger(__name__)

_enabled = False


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
    if not _enabled:
        return

    with sentry_sdk.isolation_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_exception(exc)


def capture_message(message: str, *, level: _LogLevel = "error", **context: Any) -> None:
    """Report a message-level event (e.g. a web UI client error) to the tracker.

    No-op when error tracking is disabled.
    """
    if not _enabled:
        return

    with sentry_sdk.isolation_scope() as scope:
        for key, value in context.items():
            scope.set_extra(key, value)
        sentry_sdk.capture_message(message, level=level)
